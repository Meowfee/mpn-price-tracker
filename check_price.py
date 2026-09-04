"""
Daily price-change logger for Coles and Woolworths (via RapidAPI).

Tracked products are read from the "Main" tab of the same Google Sheet --
column A: item name, column B: category (unused), column C: Coles product
ID, column D: Woolworths product ID. Row 1 is assumed to be a header row;
products start at row 2. Add a new row there to track a new product --
no code changes needed.

For each tracked product, checks yesterday's "price changes" feed from both
retailers. If a tracked product shows up (its price moved), a row is
appended to the log tab. Products that didn't change price that day are
left alone -- this is a log-on-change history, not a daily snapshot.
"""

import datetime
import json
import os
import sys
from zoneinfo import ZoneInfo

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

RAPIDAPI_KEY = os.environ["RAPIDAPI_KEY"]
GOOGLE_SA_KEY = os.environ["GOOGLE_SA_KEY"]
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1e0xcBBg5moCatA1Wq6ija5Gu-wqXlYRD6951o_GD2RI")
LOG_TAB = os.environ.get("SHEET_TAB_NAME", "Sheet1")
PRODUCTS_TAB = os.environ.get("PRODUCTS_TAB_NAME", "Main")

# Both retailers key their "price changes" feed by an Australian calendar day.
# The job runs in UTC, so "yesterday" is computed in Sydney time to make sure
# we're asking about a day that has fully finished.
SYDNEY = ZoneInfo("Australia/Sydney")
QUERY_DATE = (datetime.datetime.now(SYDNEY) - datetime.timedelta(days=1)).date().isoformat()

RETAILERS = {
    "Woolworths": {
        "host": "woolworths-products-api.p.rapidapi.com",
        "url": "https://woolworths-products-api.p.rapidapi.com/woolworths/price-changes/",
        "id_field": "woolworths_id",
    },
    "Coles": {
        "host": "coles-product-price-api.p.rapidapi.com",
        "url": "https://coles-product-price-api.p.rapidapi.com/coles/price-changes/",
        "id_field": "coles_id",
    },
}


def get_sheets_service():
    creds_info = json.loads(GOOGLE_SA_KEY)
    creds = Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def load_products(service):
    """Read tracked products from the 'Main' tab: A=name, B=category (unused),
    C=Coles ID, D=Woolworths ID. Assumes row 1 is a header row."""
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=f"{PRODUCTS_TAB}!A2:D")
        .execute()
    )
    rows = result.get("values", [])

    products = []
    for row in rows:
        row = row + [""] * (4 - len(row))  # pad short rows so unpacking is safe
        name, _category, coles_id, woolworths_id = row[:4]
        name = name.strip()
        if not name:
            continue
        products.append(
            {
                "label": name,
                "coles_id": coles_id.strip(),
                "woolworths_id": woolworths_id.strip(),
            }
        )
    return products


def extract_id(url: str) -> str:
    """Pull the trailing numeric product ID off a retailer product URL."""
    return url.rstrip("/").split("/")[-1]


def fetch_matches(retailer, products):
    headers = {
        "x-rapidapi-host": retailer["host"],
        "x-rapidapi-key": RAPIDAPI_KEY,
    }
    matches = []
    page = 1
    page_size = 100  # API max, keeps page count down

    while True:
        params = {"date": QUERY_DATE, "page": page, "page_size": page_size}
        resp = requests.get(retailer["url"], headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])

        for entry in results:
            entry_id = extract_id(entry.get("url", ""))
            for product in products:
                target_id = product.get(retailer["id_field"], "")
                if target_id and entry_id == target_id:
                    matches.append((product["label"], entry))

        total_pages = data.get("total_pages", page)
        if page >= total_pages or not results:
            break
        page += 1

    return matches


def append_rows(service, rows):
    if not rows:
        return
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{LOG_TAB}!A:H",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def main():
    service = get_sheets_service()
    products = load_products(service)

    if not products:
        print(f"No products found in '{PRODUCTS_TAB}' tab -- nothing to check.")
        return

    print(f"Tracking {len(products)} product(s): {[p['label'] for p in products]}")

    all_rows = []
    had_error = False

    for retailer_name, retailer in RETAILERS.items():
        try:
            matches = fetch_matches(retailer, products)
        except requests.RequestException as exc:
            print(f"[{retailer_name}] request failed: {exc}", file=sys.stderr)
            had_error = True
            continue

        if not matches:
            print(f"[{retailer_name}] no tracked product appeared in {QUERY_DATE}'s price changes.")
            continue

        for label, entry in matches:
            row = [
                QUERY_DATE,
                retailer_name,
                label,
                entry.get("product_name", ""),
                entry.get("barcode") or "",
                entry.get("old_price", ""),
                entry.get("new_price", ""),
                entry.get("url", ""),
            ]
            all_rows.append(row)
            print(f"[{retailer_name}] price change logged: {row}")

    append_rows(service, all_rows)

    if had_error:
        sys.exit(1)  # marks the Actions run as failed so you get notified


if __name__ == "__main__":
    main()
