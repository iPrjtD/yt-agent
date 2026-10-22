"""Probe which Gemini-API models can write a Short script right now (dev helper for choosing fallbacks)."""

import sys
import time

import litellm

from ytagent.config import get_settings
from ytagent.script import ShortScript

key = get_settings().env.gemini_api_key
schema = {"type": "json_schema",
          "json_schema": {"name": "ShortScript", "schema": ShortScript.model_json_schema(), "strict": True}}
prompt = "Write a 120-word YouTube Short script about tongue prints being unique. pillar facts."
for name in sys.argv[1:]:
    start = time.time()
    try:
        resp = litellm.completion(model=f"gemini/{name}", api_key=key, timeout=60, response_format=schema,
                                  messages=[{"role": "user", "content": prompt}])
        script = ShortScript.model_validate_json(resp.choices[0].message.content)
        print(f"{name:24} OK   {time.time() - start:5.1f}s  words={script.word_count()}")
    except Exception as exc:
        reason = " ".join(str(exc).split())
        print(f"{name:24} FAIL {time.time() - start:5.1f}s  {type(exc).__name__}: {reason[80:180]}")
