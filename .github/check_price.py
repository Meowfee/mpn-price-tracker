"""
Daily price-change logger for Coles and Woolworths (via RapidAPI).

For each tracked product, checks yesterday's "price changes" feed from both
retailers. If a tracked product shows up (meaning its price moved), a row is
appended to the Google Sheet. Products that didn't change price that day are
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

# ---------------------------------------------------------------------------
# Products being tracked. Add more entries here later -- this POC has just one.
# `woolworths_id` / `coles_id` are the static numeric product IDs pulled from
# each retailer's own product URL (confirmed manually against the barcode).
# ---------------------------------------------------------------------------
PRODUCTS = [
    {
        "label": "Don Pepperoni Salami 200g",
        "barcode": "93389389",
        "woolworths_id": "64117",
        "coles_id": "5643960",
    },
]

RAPIDAPI_KEY = os.environ["RAPIDAPI_KEY"]
GOOGLE_SA_KEY = os.environ["GOOGLE_SA_KEY"]
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1e0xcBBg5moCatA1Wq6ija5Gu-wqXlYRD6951o_GD2RI")
SHEET_TAB = os.environ.get("SHEET_TAB_NAME", "Sheet1")

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


def extract_id(url: str) -> str:
    """Pull the trailing numeric product ID off a retailer product URL."""
    return url.rstrip("/").split("/")[-1]


def fetch_matches(retailer_name, retailer):
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
            entry_barcode = str(entry.get("barcode") or "")
            for product in PRODUCTS:
                target_id = product[retailer["id_field"]]
                barcode_hit = entry_barcode and entry_barcode == product["barcode"]
                id_hit = entry_id == target_id
                if barcode_hit or id_hit:
                    matches.append((product["label"], entry))

        total_pages = data.get("total_pages", page)
        if page >= total_pages or not results:
            break
        page += 1

    return matches


def append_rows(rows):
    if not rows:
        return
    creds_info = json.loads(GOOGLE_SA_KEY)
    creds = Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds)
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A:H",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def main():
    all_rows = []
    had_error = False

    for retailer_name, retailer in RETAILERS.items():
        try:
            matches = fetch_matches(retailer_name, retailer)
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

    append_rows(all_rows)

    if had_error:
        sys.exit(1)  # marks the Actions run as failed so you get notified


if __name__ == "__main__":
    main()
