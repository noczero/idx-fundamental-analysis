# IDX Fundamental Analysis

## Description

IDX Fundamental Analysis project aims to retrieve and analyse fundamental stock data of companies listed on the
Indonesian
Stock Exchange (IDX). It fetches stock data and key statistics using Camoufox (a stealth Firefox browser), requests, and
various provider APIs, and stores the resultant data in Google Sheets or local Excel file for easy access and analysis.

https://github.com/user-attachments/assets/13395cf7-1e3e-4153-8d20-40d4755a4c6d

## Features

- **Fetch Stock Data from IDX**: Use Camoufox (stealth Firefox) to scrape stock data from IDX past Cloudflare.
- **Retrieve Fundamental Data**: Obtain key statistics and fundamental data using StockBit and YFinance API.
- **Google Sheets Integration**: Create and update Google Sheets with stock data using Google Drive API. Required
  Google Service Account environment variable.
- **Save as Excel**: Stored the fundamental analysis data in your local file.
- **Store to SQLite**: Stored all data to persistent storage.
- **Logging**: Robust logging using Loguru for debugging and tracking purposes.

## Installation

### Prerequisites

- [Python 3.14](https://docs.python.org/3.14/whatsnew/3.14.html)
- [UV](https://docs.astral.sh/uv/getting-started/installation/)

### Steps

1. Clone the repository:

   ```bash
   git clone https://github.com/noczero/idx-fundamental.git
   cd idx-fundamental
   ```

2. Install dependencies:

   ```bash
   uv sync
   ```

3. Download the Camoufox browser binary (a stealth Firefox fork used for
   scraping IDX and the Stockbit login). This is a one-time download of the
   browser + fingerprint data:

   ```bash
   uv run python -m camoufox fetch
   ```

   > On a headless Linux server you may also need Firefox's system libraries
   > (e.g. `libgtk-3-0`, `libx11-xcb1`, `libasound2`). If the browser fails to
   > start, install them via your package manager.

4. Set up environment variables:

   Create a `.env` file in the project root directory and add the following environment variables:

   ```env
   GOOGLE_SERVICE_ACCOUNT='{
     "type": "service_account",
     "project_id": "...",
     "private_key_id": "...",
     "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
     "client_email": "...",
     "client_id": "...",
     "auth_uri": "https://accounts.google.com/o/oauth2/auth",
     "token_uri": "https://oauth2.googleapis.com/token",
     "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
     "client_x509_cert_url": "..."
   }'

   GOOGLE_DRIVE_EMAILS='["email1@gmail.com", "email2@gmail.com"]'
   ```

## Usage

1. Run the main script:

   ```bash
   python main.py -f -o excel
   ```

   - The `-f` or `--full-retrieve` argument is optional. If included, the script will retrieve full stock data from
     IDX.
     If not set, it only retrieve first page which is only 10 stocks.

   - The `-o` or `--output-format` argument with two choices: `spreadsheet` and `excel`. Output will be saved into
     Google
     Sheet or Excel local file inside `output` folder.
   - This will start the process of fetching stock data from IDX, retrieving key statistics from StockBit, and
     inserting them into a Google Sheet.

## Configuration

The primary configuration options include environment variables set in the `.env` file. Ensure you have authenticated
and authorized access to Google Drive and possessed a valid username and password for StockBit API access.

## Stockbit authentication (local login + headless server)

Stockbit access tokens are captured through a real browser login (using Camoufox, a stealth Firefox fork) and are valid
for **24 hours**. To run the analysis on a browser-less server, the login is done **once locally** and the server then
renews tokens on its own using the **refresh token** — no browser required.

### Token files

Three files are stored in `STOCKBIT_TOKEN_DIR` (defaults to the system temp dir):

| File                             | Purpose                                   |
| -------------------------------- | ----------------------------------------- |
| `stockbit_token.tmp`             | Access token (24h lifetime)               |
| `stockbit_refresh_token.tmp`     | Refresh token (renews the access token)   |
| `stockbit_ua.tmp`                | Browser User-Agent (must match the token) |

Relevant environment variables:

| Variable                          | Default        | Description                                                                                     |
| --------------------------------- | -------------- | ----------------------------------------------------------------------------------------------- |
| `STOCKBIT_TOKEN_DIR`              | system temp    | Persistent directory for the token files. Set this on a server where `/tmp` is wiped on reboot. |
| `STOCKBIT_DISABLE_BROWSER_LOGIN` | *(unset)*      | Set to `1`/`true` on a headless server so the client renews only via refresh token, never launching a browser. |

### 1. Bootstrap locally (machine with a display)

```bash
# Set a stable dir so you know where the files land (optional; defaults to temp).
export STOCKBIT_TOKEN_DIR="$HOME/.idx-fundamental-tokens"
uv run python main.py --stockbit-login
```

A Camoufox (Firefox) window opens. Log in to Stockbit, wait for the dashboard, then press Enter in the terminal. The
command writes the three token files and prints their paths.

### 2. Sync the token files to the server

```bash
rsync -av "$HOME/.idx-fundamental-tokens/" user@your-vps:/var/lib/idx-fundamental/tokens/
```

### 3. Run on the server (no interactive browser)

```bash
export STOCKBIT_TOKEN_DIR=/var/lib/idx-fundamental/tokens
export STOCKBIT_DISABLE_BROWSER_LOGIN=1
uv run python main.py -f -o excel
```

The server validates the access token on startup; when it expires it silently calls Stockbit's `login/refresh` to get a
fresh one and rotates the stored refresh token. It only fails — with an actionable message — if the refresh token itself
expires, at which point you re-run step 1 and re-sync.

## Contribution Guidelines

Contributions are welcome! Feel free to open issues or submit pull requests. Please follow these guidelines:

1. Fork the repository.
2. Create a new branch for your feature/bug fix.
3. Make your changes, ensuring tests pass.
4. Open a pull request with a detailed description of your changes.

## Testing

Currently, the project does not include unit tests. However, testing can be done by running the `main.py` script and
verifying the output in the Google Sheet if argument `-o spreadsheet` is used. If argument `-o excel` is used, the output
will be saved in the `./output` folder.

## Result

### File [Limited only 10 stocks]

- [IDX Fundamental Analysis 05-10-2024](https://drive.zeroinside.id/s/GpcEYfc2MAbCS4N)

### Screencast

![demo-idx-fundamental](https://github.com/user-attachments/assets/c365cd75-fed7-41c9-8719-17bf36dc97cb)

## TODO

1. Dashboard support using Streamlit.
2. Sentiment Analysis using LLMs, will use OpenAI and Ollama.
3. Time Series Forecasting using ETS, ARIMA, and Prophet.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Acknowledgements

- [Stockbit](https://stockbit.com) for IDX composite key statistics and stock prices data.
- [Camoufox](https://github.com/daijro/camoufox) for stealth browser scraping capabilities.
- [Loguru](https://github.com/Delgan/loguru) for logging.
- [yfinance](https://github.com/ranaroussi/yfinance) for financial data.
- [Google APIs](https://developers.google.com/api-client-library/python/) for integration with Google Sheets.
