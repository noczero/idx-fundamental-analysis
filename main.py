import argparse
import sys
import time
from datetime import date

from dotenv import load_dotenv

from builders.analysers import Analyser
from builders.database_builder import DatabaseBuilder
from db import database
from providers.idx import IDX
from providers.stockbit import StockBit
from schemas.builder import BuilderOutputType
from services.stockbit_api_client import (
    StockbitApiClient,
    StockbitReauthRequiredError,
)
from utils.logger_config import logger

load_dotenv()


def parse_arguments():
    parser = argparse.ArgumentParser(description="IDX Composite Fundamental Analysis")
    parser.add_argument(
        "-f",
        "--full-retrieve",
        action="store_true",
        help="Retrieve full stock data from IDX",
    )
    parser.add_argument(
        "-o",
        "--output-format",
        type=BuilderOutputType,
        choices=list(BuilderOutputType),
        default=BuilderOutputType.SPREADSHEET,
        help="Specify the output format: 'spreadsheet' for Google Spreadsheet, 'excel' for Excel file",
    )
    parser.add_argument(
        "--stockbit-login",
        action="store_true",
        help="Run the interactive Stockbit browser login to capture access + refresh "
        "tokens, then exit. Run this locally (needs Chrome) and sync the token files "
        "to the server.",
    )
    return parser.parse_args()


def stockbit_login():
    """One-time interactive bootstrap: capture tokens for later browser-free use."""
    logger.info("Starting Stockbit interactive login (bootstrap)...")
    client = StockbitApiClient(auto_authenticate=False)
    if client.bootstrap_login():
        logger.info("Stockbit login successful. Token files written to:")
        logger.info(f"  - {client.token_temp_file_path}")
        logger.info(f"  - {client.refresh_token_temp_file_path}")
        logger.info(f"  - {client.ua_temp_file_path}")
        logger.info(
            "Sync these three files to the server's STOCKBIT_TOKEN_DIR to run "
            "browser-free there."
        )
    else:
        logger.error("Stockbit login failed. No tokens were captured.")


def main():
    logger.info("IDX Composite Fundamental Analysis")
    start_time = time.time()

    args = parse_arguments()

    if args.stockbit_login:
        stockbit_login()
        return

    # Setup database
    database.setup_db(is_drop_table=False)

    # Retrieve stocks from IDX
    idx = IDX(is_full_retrieve=args.full_retrieve)
    stocks = idx.stocks()
    logger.debug("Stocks: {}".format(stocks))
    logger.info("Total Stocks: {}".format(len(stocks)))

    # Process stocks key statistics, price, fundamental, and stream data (news) from Stockbit
    StockBit(stocks=stocks).with_stock_price().with_fundamental().with_stream_data()

    # Analyser to build the output
    title = f"IDX Fundamental Analysis {date.today().strftime('%Y-%m-%d')}"
    Analyser(stocks=stocks).build(output=args.output_format, title=title)

    # Populate to database
    database_builder = DatabaseBuilder(stocks=stocks)
    database_builder.update_or_insert_stock()
    database_builder.insert_key_statistic()
    database_builder.insert_key_analysis()
    database_builder.insert_stock_price()
    database_builder.insert_sentiment()

    elapsed = time.time() - start_time
    elapsed_minutes = elapsed / 60
    logger.info(f"Elapsed time: {elapsed_minutes:.2f} minutes")


if __name__ == "__main__":
    try:
        main()
    except StockbitReauthRequiredError as e:
        # Headless server with a dead refresh token and no browser login: stop
        # immediately with an actionable message instead of failing every request.
        logger.error(str(e))
        logger.error("Aborting run — Stockbit re-authentication required.")
        sys.exit(1)
