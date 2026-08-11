import requests

NKOD_GRAPHQL = "https://data.gov.cz/graphql"

# Oficiální standard pro úřední desky
UREDNI_DESKY_OFN = "https://ofn.gov.cz/úřední-desky/2021-07-20/"


def main():
    print("======================================")
    print("  ÚŘEDNÍ DESKA HLÍDAČ")
    print("  Vyhledávání úředních desek")
    print("======================================")
    print()

    query = """
    query {
      datasets(
        limit: 100
        filters: {
          conformsTo: "https://ofn.gov.cz/úřední-desky/2021-07-20/"
        }
      ) {
        data {
          iri
          title {
            cs
          }
          publisher {
            title {
              cs
            }
          }
          distribution {
            accessURL
            format
          }
        }
        pagination {
          totalCount
        }
      }
    }
    """

    print("Hledám datové sady úředních desek v NKOD...")
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

        print(f"Celkem nalezených úředních desek: {total}")
        print()

        print("======================================")
        print("SEZNAM ÚŘEDNÍCH DESEK")
        print("======================================")
        print()

        for number, dataset in enumerate(data, start=1):
            title = dataset.get("title") or {}
            publisher = dataset.get("publisher") or {}
            
            title_cs = title.get("cs") or "(bez názvu)"
            publisher_title = publisher.get("title") or {}
            publisher_cs = publisher_title.get("cs") or "(neznámý poskytovatel)"

            print(f"{number}. {title_cs}")
            print(f"   Poskytovatel: {publisher_cs}")
            print(f"   IRI: {dataset['iri']}")

            distributions = dataset.get("distribution") or []

            for distribution in distributions:
                if distribution.get("accessURL"):
                    print(f"   Data: {distribution['accessURL']}")

            print()

    except Exception as e:
        print("Nastala chyba:")
        print(e)


if __name__ == "__main__":
    main()
