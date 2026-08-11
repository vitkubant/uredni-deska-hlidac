import requests

NKOD_API = "https://data.gov.cz/api/v1/catalogs"


def main():
    print("======================================")
    print("  ÚŘEDNÍ DESKA HLÍDAČ")
    print("  První test")
    print("======================================")
    print()

    print("Připojuji se k NKOD...")

    try:
        response = requests.get(NKOD_API, timeout=30)
        response.raise_for_status()

        print("Připojení funguje.")
        print(f"HTTP status: {response.status_code}")
        print()

        data = response.json()

        print("Odpověď z NKOD byla úspěšně načtena.")
        print(f"Počet nalezených katalogů: {len(data)}")

    except Exception as e:
        print("Nastala chyba:")
        print(e)


if __name__ == "__main__":
    main()
