#!/bin/bash

echo "Cleaning files only (keeping folders)..."

rm -f instance/*
rm -f data/*
rm -rf migrations/*
touch migrations/__init__.py
rm -f logs/*
rm -f output/*
rm -f input/*

find . -type d -name "__pycache__" -exec rm -rf {} +
find . -type f -name "*.pyc" -delete

echo "Rebuilding DB..."

flask db init
flask db migrate -m "fresh start"
flask db upgrade

echo "Done ✅ Fresh project (folders preserved)"
