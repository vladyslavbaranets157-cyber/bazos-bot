#!/bin/bash
# Update new-price reference from NAY.sk on this Mac and publish it for the bot.
cd "$(dirname "$0")" && python3 update_prices.py && git add newprices.json && git commit -q -m "Update new prices" && git push -q origin main && echo "published"
