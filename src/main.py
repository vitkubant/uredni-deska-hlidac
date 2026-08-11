```python
import csv
import io
import json
import os
import re
import smtplib
import unicodedata
import zipfile
from datetime import date, datetime, timedelta
from email.message import EmailMessage

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


NKOD_GRAPHQL = "https://data.gov.cz/graphql"

RUIAN_URL = (
    "https://services.cuzk.cz/sestavy/cis/UI_OBEC.zip"
)

# NKOD používá u úředních desek obě varianty OFN URI.
UREDNI_DESKY_OFN = (
    "https://ofn.gov.cz/úřední-desky/2021-07-20/",
    "https://ofn.gov.cz/úřední-desky/",
)

PARDUBICKY_OKRESY = {
    "3603",  # Chrudim
    "3606",  # Pardubice
    "3609",  # Svitavy
    "3611",  # Ústí nad Orlicí
}

MAX_STARI_DNI = 30

USER_AGENT = (
    "UredniDeskaHlidac/1.0 "
    "(monitoring verejnych urednich desek)"
)


def bez_diakritiky(text):
    text = text or ""
    text = unicodedata.normalize("NFKD", str(text))
    return "".join(
        znak
        for znak in text
        if not unicodedata.combining(znak)
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
    print("Stahuji obce Pardubickeho kraje...")
    print()

    # ---------------------------------------------------------
    # 1. RÚIAN - seznam obcí
    # ---------------------------------------------------------

    response = session.get(
        RUIAN_URL,
        timeout=60,
    )
    response.raise_for_status()

    with zipfile.ZipFile(
        io.BytesIO(response.content)
    ) as archive:

        csv_soubory = [
            x
            for x in archive.namelist()
            if x.lower().endswith(".csv")
        ]

        if not csv_soubory:
            raise RuntimeError(
                "V RUIAN ZIP nebyl nalezen CSV soubor."
            )

        raw = archive.read(csv_soubory[0])

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
            "CSV se nepodarilo dekodovat."
        )

    reader = csv.DictReader(
        io.StringIO(text),
        delimiter=";",
    )

    obce = []

    for row in reader:
        kod = (
            row.get("KOD") or ""
        ).strip()

        nazev = (
            row.get("NAZEV") or ""
        ).strip()

        okres = (
            row.get("OKRES_KOD") or ""
        ).strip()

        plati_do = (
            row.get("PLATI_DO") or ""
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
                "normalizovany_nazev": bez_diakritiky(
                    nazev
                ),
                "ico": "",
            }
        )

    print(
        f"Nalezeno obci: {len(obce)}"
    )

    return obce


def graphql_dotaz(session, ofn_uri):
    query = f"""
    query {{
      datasets(
        limit: 1000
        filters: {{
          conformsTo: "{ofn_uri}"
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

    if not response.ok:
        print(
            f"  NKOD HTTP chyba: "
            f"{response.status_code}"
        )
        print(
            f"  Odpoved serveru: "
            f"{response.text[:1000]}"
        )
        return None

    try:
        result = response.json()
    except ValueError:
        print(
            "  NKOD vratil neplatny JSON."
        )
        return None

    if "errors" in result:
        print("  GraphQL chyba:")
        print(
            json.dumps(
                result["errors"],
                indent=2,
                ensure_ascii=False,
            )
        )
        return None

    datasets = (
        (result.get("data") or {})
        .get("datasets")
    )

    if not datasets:
        return None

    return datasets


def extrahuj_ico_z_iri(iri):
    """
    Pokusí se získat IČO z IRI publishera.

    Očekáváme osmimístné IČO.
    Nejprve hledáme osm číslic na konci IRI,
    potom obecně osm číslic uvnitř IRI.
    """

    iri = str(iri or "").strip()

    if not iri:
        return ""

    match = re.search(
        r"(?<!\d)(\d{8})(?!\d)$",
        iri,
    )

    if match:
        return match.group(1)

    match = re.search(
        r"(?<!\d)(\d{8})(?!\d)",
        iri,
    )

    if match:
        return match.group(1)

    return ""


def nacti_uredni_desky(session):
    print()
    print(
        "Stahuji metadata urednich desek z NKOD..."
    )

    vsechny = {}
    uspesne_dotazy = 0

    for ofn_uri in UREDNI_DESKY_OFN:
        print(
            f"  Zkousim OFN: {ofn_uri}"
        )

        datasets = graphql_dotaz(
            session,
            ofn_uri,
        )

        if datasets is None:
            print(
                "  Dotaz se nepodaril."
            )
            continue

        uspesne_dotazy += 1

        celkem = (
            datasets["pagination"]
            ["totalCount"]
        )

        data = (
            datasets.get("data")
            or []
        )

        print(
            f"  NKOD vratil: {celkem} datovych sad"
        )
        print(
            f"  Nacteno v tomto dotazu: "
            f"{len(data)}"
        )

        for deska in data:
            iri = (
                deska.get("iri")
                or ""
            )

            publisher = (
                deska.get("publisher")
                or {}
            )

            publisher_iri = (
                publisher.get("iri")
                or ""
            )

            # -------------------------------------------------
            # IČO publishera.
            #
            # Důležité:
            # IČO se ukládá přímo k datasetu a následně má
            # v párování přednost před názvem.
            # -------------------------------------------------

            deska["_publisher_ico"] = (
                extrahuj_ico_z_iri(
                    publisher_iri
                )
            )

            if iri:
                vsechny[iri] = deska

    if uspesne_dotazy == 0:
        raise RuntimeError(
            "Nepodarilo se uskutecnit zadny NKOD dotaz."
        )

    desky = list(
        vsechny.values()
    )

    print(
        f"NKOD urednich desek celkem po slouceni: "
        f"{len(desky)}"
    )

    return desky


def najdi_kandidaty(obce, desky):
    """
    Bezpečně přiřadí úřední desku ke konkrétní obci.

    PRIORITA PÁROVÁNÍ:

    1. IČO publishera
    2. pokud publisher IČO NEMÁ -> bezpečné párování názvem
       publishera
    3. pokud publisher IČO NEMÁ a publisher nestačí ->
       bezpečné párování názvem datasetu

    DŮLEŽITÉ:
    Pokud publisher IČO MÁ, ale toto IČO nepatří žádné
    z našich obcí, už se NESMÍ pokračovat na párování
    podle názvu.

    Tím se zabrání například tomu, aby:
      Ministerstvo spravedlnosti
      Statutární město Kladno
      Město Hodonín
      Město Rudná

    byly omylem přiřazeny k obci v Pardubickém kraji
    jen proto, že název datasetu obsahuje název obce
    nebo okresu.
    """

    kandidati = []

    # ---------------------------------------------------------
    # Pomocná funkce pro rychlé hledání obce podle IČO.
    # ---------------------------------------------------------

    obec_podle_ico = {}

    for obec in obce:
        ico = (
            obec.get("ico")
            or ""
        ).strip()

        if ico:
            obec_podle_ico[ico] = obec

    def cisti_text(text):
        text = bez_diakritiky(text)

        text = (
            text
            .replace("–", "-")
            .replace("—", "-")
        )

        text = re.sub(
            r"\s+",
            " ",
            text,
        )

        return text.strip()

    def priprav_varianty(obec):
        nazev = cisti_text(
            obec["nazev"]
        )

        return {
            nazev,
            f"obec {nazev}",
            f"mesto {nazev}",
            f"mestys {nazev}",
            f"statutarni mesto {nazev}",
            f"obecni urad {nazev}",
            f"mestsky urad {nazev}",
            f"urad mesta {nazev}",
            f"urad obce {nazev}",
            f"magistrat mesta {nazev}",
            f"magistrat {nazev}",
            f"uredni deska {nazev}",
            f"uredni deska mesta {nazev}",
            f"uredni deska obce {nazev}",
            f"uredni deska mestyse {nazev}",
            f"uredni deska uradu {nazev}",
        }

    def obsahuje_celou_frazi(text, fraze):
        text = cisti_text(text)
        fraze = cisti_text(fraze)

        if not text or not fraze:
            return False

        if text == fraze:
            return True

        vzor = (
            r"(?<![a-z0-9])"
            + re.escape(fraze)
            + r"(?![a-z0-9])"
        )

        return (
            re.search(
                vzor,
                text,
            )
            is not None
        )

    def je_zakazany_subjekt(text):
        text = cisti_text(text)

        zakazane = (
            "krajsky urad",
            "kraj ",
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

        return any(
            text.startswith(zakaz)
            for zakaz in zakazane
        )

    def odpovida_obci(text, obec):
        text = cisti_text(text)

        if not text:
            return False

        if je_zakazany_subjekt(text):
            return False

        nazev = cisti_text(
            obec["nazev"]
        )

        varianty = priprav_varianty(
            obec
        )

        # -----------------------------------------------------
        # 1. Přesná shoda celého názvu.
        # -----------------------------------------------------

        if text in varianty:
            return True

        # -----------------------------------------------------
        # 2. Běžné prefixy.
        # -----------------------------------------------------

        prefixy = (
            "uredni deska - ",
            "uredni deska ",
            "elektronicka uredni deska - ",
            "elektronicka uredni deska ",
            "uredni deska mesta ",
            "uredni deska obce ",
            "uredni deska mestyse ",
            "uredni deska uradu ",
            "mesto ",
            "obec ",
            "mestys ",
            "statutarni mesto ",
            "mestsky urad ",
            "obecni urad ",
            "urad mesta ",
            "urad obce ",
            "magistrat mesta ",
            "magistrat ",
        )

        for prefix in prefixy:
            if text.startswith(prefix):
                zbytek = (
                    text[len(prefix):]
                    .strip()
                )

                if zbytek == nazev:
                    return True

        # -----------------------------------------------------
        # 3. U víceslovných názvů povolíme bezpečnou
        #    shodu celé fráze.
        # -----------------------------------------------------

        pocet_slov = len(
            nazev.split()
        )

        if pocet_slov >= 2:
            if obsahuje_celou_frazi(
                text,
                nazev,
            ):
                return True

        return False

    # ---------------------------------------------------------
    # Projdeme jednotlivé datasety NKOD.
    # ---------------------------------------------------------

    for deska in desky:

        title = (
            (deska.get("title") or {})
            .get("cs")
            or ""
        )

        publisher_data = (
            deska.get("publisher")
            or {}
        )

        publisher = (
            (publisher_data.get("title") or {})
            .get("cs")
            or ""
        )

        # -----------------------------------------------------
        # 1. IČO publishera.
        # -----------------------------------------------------

        publisher_ico = (
            deska.get("_publisher_ico")
            or ""
        ).strip()

        nalezena_obec = None

        if publisher_ico:

            # -------------------------------------------------
            # Pokud IČO známe, je to AUTHORITATIVNÍ párování.
            # -------------------------------------------------

            nalezena_obec = obec_podle_ico.get(
                publisher_ico
            )

            # -------------------------------------------------
            # KRITICKÁ OCHRANA:
            #
            # Publisher má IČO, ale IČO nepatří žádné
            # sledované obci.
            #
            # V takovém případě dataset ZAMÍTNEME.
            #
            # Nesmíme následně zkoušet název, protože právě
            # tím vznikaly falešné výsledky typu Kladno,
            # Hodonín nebo Ministerstvo spravedlnosti.
            # -------------------------------------------------

            if nalezena_obec is None:
                continue

        # -----------------------------------------------------
        # 2. Pokud publisher IČO vůbec nemá, použijeme
        #    dosavadní bezpečné párování podle publishera.
        # -----------------------------------------------------

        if (
            nalezena_obec is None
            and not publisher_ico
        ):
            for obec in obce:
                if odpovida_obci(
                    publisher,
                    obec,
                ):
                    nalezena_obec = obec
                    break

        # -----------------------------------------------------
        # 3. Pokud publisher IČO nemá a publisher nestačí,
        #    zkusíme název datasetu.
        # -----------------------------------------------------

        if (
            nalezena_obec is None
            and not publisher_ico
        ):
            for obec in obce:
                if odpovida_obci(
                    title,
                    obec,
                ):
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

        format_text = str(
            format_data
        ).lower()

        if (
            "json" not in format_text
            and not url.lower().endswith(
                (
                    ".json",
                    ".jsonld",
                )
            )
        ):
            continue

        try:
            response = session.get(
                url,
                timeout=30,
            )

            response.raise_for_status()

            try:
                return response.json()

            except ValueError:
                print(
                    "      Server vratil "
                    "neplatny JSON."
                )
                print(
                    "      Content-Type: "
                    f"{response.headers.get('Content-Type', '')}"
                )
                print(
                    f"      HTTP: "
                    f"{response.status_code}"
                )
                continue

        except requests.exceptions.SSLError:
            print(
                "      SSL chyba."
            )
            print(
                "      Zkousim pripojeni "
                "bez overeni certifikatu..."
            )

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
                    print(
                        "      Ani druhy pokus "
                        "nevratil JSON."
                    )
                    continue

            except Exception as druhy_error:
                print(
                    "      Druhy pokus selhal: "
                    f"{druhy_error}"
                )
                continue

        except requests.exceptions.RequestException as error:
            print(
                f"      Chyba pripojeni: {error}"
            )
            continue

        except Exception as error:
            print(
                f"      Neocekavana chyba: {error}"
            )
            continue

    return None


def ziskej_informace(data):
    if not isinstance(data, dict):
        return []

    informace = (
        data.get("informace")
        or []
    )

    if isinstance(
        informace,
        dict,
    ):
        informace = [informace]

    return informace


def ziskej_text_cs(hodnota):
    if isinstance(hodnota, dict):
        return (
            hodnota.get("cs")
            or hodnota.get("cze")
            or hodnota.get("cs-CZ")
            or ""
        )

    return str(
        hodnota or ""
    )


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

                klic_text = bez_diakritiky(
                    str(klic)
                )

                if (
                    "vyves" in klic_text
                    or "zverej" in klic_text
                ):
                    if hodnota:
                        nalezene.append(
                            hodnota
                        )

                projdi(hodnota)

        elif isinstance(obj, list):

            for hodnota in obj:
                projdi(hodnota)

    projdi(item)

    return (
        nalezene[0]
        if nalezene
        else None
    )


def je_aktualni_datum(datum):
    if not datum:
        return False

    datum_text = str(
        datum
    ).strip()

    if datum_text in (
        "?",
        "",
        "None",
        "null",
    ):
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

    hranice = (
        dnes
        - timedelta(
            days=MAX_STARI_DNI
        )
    )

    return (
        hranice
        <= datum_obj
        <= dnes
    )


def je_aktualni_nabidka_pozemku(item):
    if not isinstance(item, dict):
        return False

    nazev = ziskej_text_cs(
        item.get("název")
    )

    text = bez_diakritiky(
        nazev
    )

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

    if any(
        slovo in text
        for slovo in nechtene
    ):
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

    if any(
        slovo in text
        for slovo in nechtene_obchody
    ):
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

    return (
        prodej
        or vyberove
        or aukce
    )


def odesli_email(nalezene):
    smtp_host = (
        os.getenv("SMTP_HOST")
        or "smtp.gmail.com"
    )

    smtp_port_text = (
        os.getenv("SMTP_PORT")
        or "587"
    )

    smtp_user = (
        os.getenv("SMTP_USER")
        or ""
    )

    smtp_password = (
        os.getenv("SMTP_PASSWORD")
        or ""
    )

    mail_to = (
        os.getenv("MAIL_TO")
        or ""
    )

    if (
        not smtp_user
        or not smtp_password
        or not mail_to
    ):
        print()
        print(
            "E-mail neposilam: chybi "
            "SMTP_USER, SMTP_PASSWORD "
            "nebo MAIL_TO."
        )
        return False

    try:
        smtp_port = int(
            smtp_port_text
        )

    except ValueError:
        print(
            "Chybna hodnota SMTP_PORT: "
            f"{smtp_port_text!r}"
        )
        return False

    msg = EmailMessage()

    msg["Subject"] = (
        "Uredni deska hlidac - "
        f"{len(nalezene)} nabidek pozemku"
    )

    msg["From"] = smtp_user
    msg["To"] = mail_to

    if nalezene:

        radky = [
            "Nalezene aktualni "
            "nabidky pozemku "
            "v Pardubickem kraji:",
            "",
        ]

        for cislo, item in enumerate(
            nalezene[:100],
            start=1,
        ):

            radky.append(
                f"{cislo}. {item['nazev']}"
            )

            radky.append(
                f"Obec: "
                f"{', '.join(item['obce'])}"
            )

            radky.append(
                f"Poskytovatel: "
                f"{item['publisher']}"
            )

            radky.append(
                f"Vyveseni: "
                f"{item['datum']}"
            )

            if item["url"]:
                radky.append(
                    f"URL: {item['url']}"
                )

            radky.append("")

    else:

        radky = [
            "Hlidac dokoncil kontrolu.",
            "",
            "Aktualne nebyla nalezena "
            "zadna nabidka pozemku.",
        ]

    msg.set_content(
        "\n".join(radky)
    )

    try:
        print()
        print(
            "Posilam e-mail..."
        )

        with smtplib.SMTP(
            smtp_host,
            smtp_port,
            timeout=30,
        ) as smtp:

            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()

            smtp.login(
                smtp_user,
                smtp_password,
            )

            smtp.send_message(msg)

        print(
            "E-mail odeslan."
        )

        return True

    except Exception as error:
        print(
            "E-mail se nepodarilo "
            f"odeslat: {error}"
        )

        return False


def hlavni():
    print(
        "======================================"
    )

    print(
        " UREDNI DESKA HLIDAC"
    )

    print(
        " PARDUBICKY KRAJ"
    )

    print(
        "======================================"
    )

    print()

    session = vytvor_session()

    obce = nacti_obce(
        session
    )

    desky = nacti_uredni_desky(
        session
    )

    print()

    print(
        "Hledam mozne uredni desky "
        "obci Pardubickeho kraje..."
    )

    kandidati = najdi_kandidaty(
        obce,
        desky,
    )

    print()

    print(
        f"Kandidatnich urednich desek: "
        f"{len(kandidati)}"
    )

    print()

    for kandidat in kandidati:

        deska = kandidat["deska"]

        title = (
            (deska.get("title") or {})
            .get("cs")
            or "Bez nazvu"
        )

        publisher = (
            (deska.get("publisher") or {})
            .get("title", {})
            .get("cs")
            or "Neznamy"
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
            f"  Pravdepodobna obec: "
            f"{obce_text}"
        )

    print()

    print(
        "======================================"
    )

    print(
        " STAHUJI VYBRANE UREDNI DESKY"
    )

    print(
        "======================================"
    )

    print()

    nalezene = []

    for cislo, kandidat in enumerate(
        kandidati,
        start=1,
    ):

        deska = kandidat["deska"]

        publisher = (
            (deska.get("publisher") or {})
            .get("title", {})
            .get("cs")
            or "Neznamy"
        )

        print(
            f"[{cislo}/{len(kandidati)}] "
            f"{publisher}"
        )

        data = stahni_jsonld(
            session,
            deska,
        )

        if data is None:
            print(
                "   ✗ nepodarilo se stahnout"
            )
            continue

        informace = ziskej_informace(
            data
        )

        print(
            f"   {len(informace)} informaci"
        )

        for item in informace:

            datum = (
                ziskej_datum_vyveseni(
                    item
                )
            )

            if not je_aktualni_datum(
                datum
            ):
                continue

            if not je_aktualni_nabidka_pozemku(
                item
            ):
                continue

            nazev = (
                ziskej_text_cs(
                    item.get("název")
                )
                or "Bez nazvu"
            )

            url = (
                item.get("url")
                or ""
            )

            nalezene.append(
                {
                    "nazev": nazev,
                    "url": url,
                    "datum": datum,
                    "obce": [
                        obec["nazev"]
                        for obec
                        in kandidat["obce"]
                    ],
                    "publisher": publisher,
                }
            )

    # ---------------------------------------------------------
    # Odstranění duplicit.
    # ---------------------------------------------------------

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

    nalezene.sort(
        key=lambda item: str(
            item["datum"]
        ),
        reverse=True,
    )

    print()

    print(
        f"Aktualnich nabidek pozemku: "
        f"{len(nalezene)}"
    )

    print()

    print(
        "======================================"
    )

    print(
        " NALEZENE KANDIDATNI NABIDKY"
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
        start=1,
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
            f"   Vyveseni: "
            f"{item['datum']}"
        )

        if item["url"]:
            print(
                f"   URL: {item['url']}"
            )

        print()

    # E-mail se odesílá i při nule výsledků,
    # aby byl test jednoznačný.
    odesli_email(
        nalezene
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
```
