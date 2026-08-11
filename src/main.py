import requests

NKOD_GRAPHQL = "https://data.gov.cz/graphql"


def main():
    print("======================================")
    print("  ÚŘEDNÍ DESKA HLÍDAČ")
    print("  První připojení k NKOD")
    print("======================================")
    print()

    query = """
    {
      datasets(limit: 5) {
        data {
          iri
          title {
            cs
          }
        }
        pagination {
          totalCount
        }
      }
    }
    """

    print("Připojuji se k NKOD přes GraphQL...")

    try:
        response = requests.post(
            NKOD_GRAPHQL,
            json={"query": query},
            timeout=30
        )

        response.raise_for_status()

        result = response.json()

        if "errors" in result:
            print("NKOD vrátil chybu:")
            print(result["errors"])
            return

        datasets = result["data"]["datasets"]

        print("Připojení funguje.")
        print()

        print(f"Celkový počet datových sad v NKOD: "
              f"{datasets['pagination']['totalCount']}")
        print()

        print("Prvních 5 datových sad:")
        print("--------------------------------------")

        for dataset in datasets["data"]:
            title = dataset["title"]

            if title and title.get("cs"):
                name = title["cs"]
            else:
                name = "(bez českého názvu)"

            print(name)
            print(dataset["iri"])
            print()

    except Exception as e:
        print("Nastala chyba:")
        print(e)


if __name__ == "__main__":
    main()
