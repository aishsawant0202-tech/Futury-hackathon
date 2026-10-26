"""Quick check that the key works and both models answer. Run: python -m scripts.smoke_llm"""
import logging

from pydantic import BaseModel

from specpilot import config, llm

logging.basicConfig(level=logging.INFO, format="%(message)s")


class Translation(BaseModel):
    german: str
    english: str


if not config.LLM_API_KEY:
    raise SystemExit("No LLM_API_KEY in .env. Copy .env.example to .env and add the team key.")

print("Models on the gateway:", llm.list_models())
for model in (config.LLM_MODEL_GERMAN, config.LLM_MODEL_CODE):
    print(f"\n--- {model}")
    print(llm.chat(model, [{"role": "user", "content": "Antworte in einem Satz: Was ist eine Arbeitsunfähigkeitsbescheinigung?"}]))
    t = llm.ask_json(model, "You translate German social insurance terms.", "Translate: Vorerkrankungszeiten", Translation)
    print("JSON test:", t)
