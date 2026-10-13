"""Validate the hosting configuration without creating or purchasing resources."""
import json
from pathlib import Path
import urllib.request
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
with urllib.request.urlopen("https://render.com/schema/render.yaml.json", timeout=30) as response:
    schema = json.load(response)
configuration = yaml.safe_load((ROOT / "render.yaml").read_text())
Draft202012Validator.check_schema(schema)
Draft202012Validator(schema).validate(configuration)
print("Render Blueprint matches the official schema; no resources were created.")
