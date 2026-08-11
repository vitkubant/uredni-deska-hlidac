import requests
from collections import Counter

NKOD_GRAPHQL = "https://data.gov.cz/graphql"

UREDNI_DESKY_OFN = (
    "https://ofn.gov.cz/úřední-desky/2021-07-20/"
)


def main():
    print("======================================")
    print("  ÚŘEDNÍ DESKA HLÍDAČ")
    print("  Analýza poskytovatelů")
    print("======================================")
    print()

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

    print("Stahuji seznam úředních desek z NKOD...")
    print()

    try:
        response = requests.post(
            NKOD_GRAPHQL,
            json={"query": query},
            timeout=60
        )

        response.raise_for_status()

        result = response.json()

        if "errors" in result:
            print("NKOD vrátil chybu:")
            print(result["errors"])
            return

        datasets = result["data"]["datasets"]

        total = datasets["pagination"]["totalCount"]
        data = datasets["data"]

        print(f"Celkem úředních desek: {total}")
        print(f"Staženo záznamů: {len(data)}")
        print()

        # Seznam poskytovatelů
        publishers = []

        for dataset in data:
            publisher = dataset.get("publisher") or {}
            title = publisher.get("title") or {}

            name = title.get("cs")

            if name:
                publishers.append(name)

        counts = Counter(publishers)

        print("======================================")
        print("POSKYTOVATELÉ ÚŘEDNÍCH DESEK")
        print("======================================")
        print()

        for number, (name, count) in enumerate(
            sorted(counts.items()), start=1
        ):
            print(f"{number}. {name} ({count} datových sad)")

        print()
        print("======================================")
        print("HLEDÁNÍ PARDUBICKÉHO KRAJE")
        print("======================================")
        print()

        # Zatím pouze hledáme záznamy,
        # kde se v názvu nebo poskytovateli objeví
        # něco spojeného s Pardubickým krajem.
        hledane_vyrazy = [
            "pardub",
            "chrudim",
            "svitav",
            "ústí nad orlicí",
            "ústi nad orlici"
        ]

        nalezeno = 0

        for dataset in data:
            dataset_title = (
                (dataset.get("title") or {}).get("cs") or ""
            )

            publisher_title = (
                (dataset.get("publisher") or {})
                .get("title", {})
                .get("cs") or ""
            )

            text = (
                dataset_title + " " + publisher_title
            ).lower()

            if any(vyraz in text for vyraz in hledane_vyrazy):
                nalezeno += 1

                print(f"{nalezeno}. {dataset_title}")
                print(f"   Poskytovatel: {publisher_title}")
                print(f"   IRI: {dataset['iri']}")
                print()

        print(
            f"Nalezeno kandidátů podle názvu: {nalezeno}"
        )

    except Exception as e:
        print("Nastala chyba:")
        print(e)


if __name__ == "__main__":
    main()
