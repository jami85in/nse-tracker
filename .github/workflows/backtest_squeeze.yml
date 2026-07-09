name: Squeeze Backtest (resumable)

# Re-simulates the squeeze strategy over the full OHLC history and writes
# data/backtest/squeeze_trades.json (baseline) + squeeze_report.json (summary).
# Resumable: after the first full run it only re-computes the recent tail, so
# subsequent runs are cheap. Not needed daily — strategy validation is slow-
# moving. Run manually after a data refresh, or on the weekly schedule below.

on:
  workflow_dispatch:
  schedule:
    - cron: "30 4 * * 6"   # Saturdays 10:00 IST — after the week's data is in

permissions:
  contents: write

concurrency:
  group: squeeze-backtest
  cancel-in-progress: false

jobs:
  backtest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install pandas numpy

      - name: Run squeeze backtest
        run: python scripts/backtest_squeeze.py

      - name: Commit results
        run: |
          git config user.name "nse-tracker-bot"
          git config user.email "actions@github.com"
          git add data/backtest/squeeze_trades.json data/backtest/squeeze_report.json
          git diff --staged --quiet || git commit -m "backtest(squeeze): refresh $(date -u +'%Y-%m-%d')"
          git pull --rebase --autostash origin main || true
          git push
